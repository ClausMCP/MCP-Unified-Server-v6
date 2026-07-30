#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Media v3.1 (Extended)
Universal media metadata extraction: EXIF (photos), ID3/FLAC/OGG/M4A (music), 
ffprobe (video), OCR text extraction, thumbnail generation.
Integration with auto_sort for smart media categorization.
"""
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Union
from datetime import datetime
from fractions import Fraction
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Universal Audio Metadata ──────────────────────────────────────────────
def _extract_audio_metadata_mutagen(path: Path) -> Dict:
    """Extract metadata from any audio format using mutagen."""
    try:
        from mutagen import File as MutagenFile
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.oggvorbis import OggVorbis
        from mutagen.mp4 import MP4
        from mutagen.id3 import ID3
    except ImportError:
        return {"error": "mutagen library not installed. Install with: pip install mutagen"}
    
    try:
        audio = MutagenFile(str(path), easy=False)
        if audio is None:
            return {"error": "Unsupported audio format"}
        
        result = {
            "duration_sec": round(audio.info.length, 2) if hasattr(audio, 'info') and audio.info else None,
            "bitrate": getattr(audio.info, 'bitrate', None),
            "sample_rate": getattr(audio.info, 'sample_rate', None),
            "channels": getattr(audio.info, 'channels', None),
            "format": type(audio).__name__,
            "tags": {}
        }
        
        # Extract tags based on format
        if isinstance(audio, MP3) and audio.tags:
            for key, value in audio.tags.items():
                result["tags"][key] = str(value)
        elif isinstance(audio, FLAC):
            for key, value in audio.items():
                result["tags"][key] = value[0] if isinstance(value, list) and len(value) == 1 else value
        elif isinstance(audio, OggVorbis):
            for key, value in audio.items():
                result["tags"][key] = value[0] if isinstance(value, list) and len(value) == 1 else value
        elif isinstance(audio, MP4):
            for key, value in audio.items():
                if isinstance(value, (str, int, float)):
                    result["tags"][key] = value
                elif isinstance(value, list) and len(value) > 0:
                    result["tags"][key] = value[0]
        
        return result
    except Exception as e:
        return {"error": str(e)}

# ─── EXIF (photos) ──────────────────────────────────────────────────────────
def _extract_exif(path: Path) -> Dict:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
    except ImportError:
        return {"error": "Pillow library not installed. Install with: pip install Pillow"}
    
    try:
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return {"basic": {"format": img.format, "mode": img.mode, "size": img.size}}
        
        result = {"basic": {"format": img.format, "mode": img.mode, "size": img.size}}
        exif_data = {}
        gps_data = {}
        
        for tag_id, value in exif.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'GPSInfo':
                for gps_tag_id, gps_value in value.items():
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_data[gps_tag] = str(gps_value)
            else:
                exif_data[tag] = str(value) if not isinstance(value, (int, float)) else value
        
        result["exif"] = exif_data
        if gps_data:
            result["gps"] = gps_data
        
        dt = exif_data.get("DateTimeOriginal", exif_data.get("DateTime", ""))
        if dt:
            try:
                result["parsed_date"] = datetime.strptime(dt, "%Y:%m:%d %H:%M:%S").isoformat()
            except Exception:
                pass
        
        return result
    except Exception as e:
        return {"error": str(e)}

# ─── Video (ffprobe) ────────────────────────────────────────────────────────
def _safe_parse_fps(rate_str: str) -> float:
    try:
        if not rate_str:
            return 0.0
        return float(Fraction(rate_str.strip()))
    except (ValueError, ZeroDivisionError, TypeError, AttributeError):
        return 0.0

def _extract_video(path: Path) -> Dict:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path)
        ], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return {"error": "ffprobe not found. Install ffmpeg."}
    except Exception as e:
        return {"error": str(e)}
    
    if result.returncode != 0:
        return {"error": result.stderr.strip() if result.stderr else "ffprobe failed"}
    
    try:
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
        
        # Extract detailed video info
        video_info = {
            "codec": video_stream.get("codec_name", ""),
            "codec_long": video_stream.get("codec_long_name", ""),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": _safe_parse_fps(video_stream.get("r_frame_rate", "0/1")),
            "avg_fps": _safe_parse_fps(video_stream.get("avg_frame_rate", "0/1")),
            "pixel_format": video_stream.get("pix_fmt", ""),
            "bit_rate": video_stream.get("bit_rate"),
            "profile": video_stream.get("profile", ""),
            "level": video_stream.get("level"),
            "rotation": video_stream.get("tags", {}).get("rotate", "0")
        }
        
        # Extract detailed audio info
        audio_info = {
            "codec": audio_stream.get("codec_name", ""),
            "codec_long": audio_stream.get("codec_long_name", ""),
            "channels": audio_stream.get("channels"),
            "channel_layout": audio_stream.get("channel_layout", ""),
            "sample_rate": audio_stream.get("sample_rate"),
            "bit_rate": audio_stream.get("bit_rate"),
            "profile": audio_stream.get("profile", "")
        } if audio_stream else {}
        
        return {
            "duration_sec": round(float(fmt.get("duration", 0)), 2),
            "bitrate": int(fmt.get("bit_rate", 0)),
            "size_bytes": int(fmt.get("size", 0)),
            "format_name": fmt.get("format_name", ""),
            "format_long_name": fmt.get("format_long_name", ""),
            "nb_streams": len(streams),
            "video": video_info,
            "audio": audio_info
        }
    except Exception as e:
        return {"error": str(e)}

# ─── Thumbnail Generation ───────────────────────────────────────────────────
def generate_thumbnail(source_path: str, output_path: str, 
                       size: tuple = (256, 256), 
                       quality: int = 85) -> Dict:
    """Generate thumbnail for image or video file."""
    try:
        from PIL import Image
    except ImportError:
        return {"error": "Pillow library not installed. Install with: pip install Pillow"}
    
    src = Path(normalize_path(source_path))
    dst = Path(normalize_path(output_path))
    _ensure_allowed(src, "generate_thumbnail")
    _ensure_allowed(dst.parent, "generate_thumbnail")
    
    if not src.is_file():
        return {"error": f"Source file not found: {source_path}"}
    
    ext = src.suffix.lower()
    start_time = time.time()
    
    try:
        if ext in ('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'):
            # Generate thumbnail from video using ffmpeg
            temp_png = dst.parent / f".thumb_temp_{dst.stem}.png"
            cmd = [
                "ffmpeg", "-v", "quiet", "-ss", "00:00:01",
                "-i", str(src), "-vframes", "1",
                "-vf", f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease",
                str(temp_png)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return {"error": f"ffmpeg failed: {result.stderr.strip()}"}
            
            # Convert PNG to final format if needed
            img = Image.open(temp_png)
            if dst.suffix.lower() in ('.jpg', '.jpeg'):
                img = img.convert('RGB')
                img.save(str(dst), 'JPEG', quality=quality)
            else:
                img.save(str(dst), quality=quality)
            temp_png.unlink(missing_ok=True)
            
        elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.gif'):
            # Generate thumbnail from image using PIL
            img = Image.open(src)
            img.thumbnail(size, Image.Resampling.LANCZOS)
            
            if dst.suffix.lower() in ('.jpg', '.jpeg'):
                img = img.convert('RGB')
                img.save(str(dst), 'JPEG', quality=quality)
            else:
                img.save(str(dst), quality=quality)
        else:
            return {"error": f"Unsupported format for thumbnail: {ext}"}
        
        elapsed = time.time() - start_time
        thumb_size = dst.stat().st_size
        
        conversation_memory.add(
            op="generate_thumbnail",
            paths={"source": str(src), "thumbnail": str(dst)},
            status="success", dialog=dialog_ctx.get(),
            context=f"Generated {size[0]}x{size[1]} thumbnail for {src.name} ({thumb_size} bytes)"
        )
        
        return {
            "status": "success",
            "source": str(src),
            "thumbnail": str(dst),
            "size": size,
            "thumbnail_size_bytes": thumb_size,
            "elapsed_sec": round(elapsed, 2)
        }
    except Exception as e:
        return {"error": str(e)}

# ─── OCR Text Extraction ────────────────────────────────────────────────────
def extract_text_from_image(image_path: str, lang: str = "eng+rus") -> Dict:
    """Extract text from image using OCR (Tesseract)."""
    try:
        import pytesseract
        from PIL import Image
        # Автопоиск tesseract (как в mcp_pdf): работает без правки PATH на Windows.
        try:
            from mcp_shared import locate_tesseract
            _tc = locate_tesseract()
            if _tc:
                pytesseract.pytesseract.tesseract_cmd = _tc
        except Exception:
            pass
    except ImportError:
        return {"error": "pytesseract or Pillow not installed. Install with: pip install pytesseract Pillow"}
    
    p = Path(normalize_path(image_path))
    _ensure_allowed(p, "extract_text_from_image")
    
    if not p.is_file():
        return {"error": f"Image not found: {image_path}"}
    
    try:
        img = Image.open(p)
        
        # Check if tesseract is available
        try:
            pytesseract.get_tesseract_version()
        except Exception as e:
            return {"error": f"Tesseract OCR not found. Install tesseract-ocr. Details: {str(e)}"}
        
        start_time = time.time()
        text = pytesseract.image_to_string(img, lang=lang)
        elapsed = time.time() - start_time
        
        # Get detailed data
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
        
        # Filter out empty results
        words = []
        for i, word in enumerate(data['text']):
            if word.strip():
                words.append({
                    "text": word,
                    "confidence": data['conf'][i],
                    "bbox": {
                        "left": data['left'][i],
                        "top": data['top'][i],
                        "width": data['width'][i],
                        "height": data['height'][i]
                    }
                })
        
        conversation_memory.add(
            op="extract_text_from_image",
            paths={"image": str(p)},
            status="success", dialog=dialog_ctx.get(),
            context=f"Extracted {len(text)} chars from {p.name} ({len(words)} words)"
        )
        
        return {
            "path": str(p),
            "filename": p.name,
            "text": text,
            "word_count": len(words),
            "character_count": len(text),
            "words": words,
            "language": lang,
            "elapsed_sec": round(elapsed, 2)
        }
    except Exception as e:
        return {"error": str(e)}

# ─── Get Media Duration ─────────────────────────────────────────────────────
def get_media_duration(path: str) -> Dict:
    """Get duration for audio or video file."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "get_media_duration")
    
    if not p.is_file():
        return {"error": f"File not found: {path}"}
    
    ext = p.suffix.lower()
    
    try:
        if ext in ('.mp3', '.flac', '.ogg', '.m4a', '.wav', '.wma', '.aac'):
            # Use mutagen for audio
            from mutagen import File as MutagenFile
            audio = MutagenFile(str(p))
            if audio and audio.info:
                duration = audio.info.length
                return {
                    "path": str(p),
                    "type": "audio",
                    "duration_sec": round(duration, 2),
                    "duration_formatted": time.strftime('%H:%M:%S', time.gmtime(duration))
                }
            else:
                return {"error": "Could not read audio metadata"}
        
        elif ext in ('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'):
            # Use ffprobe for video
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(p)
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                return {"error": "ffprobe failed"}
            
            data = json.loads(result.stdout)
            duration = float(data.get('format', {}).get('duration', 0))
            
            return {
                "path": str(p),
                "type": "video",
                "duration_sec": round(duration, 2),
                "duration_formatted": time.strftime('%H:%M:%S', time.gmtime(duration))
            }
        else:
            return {"error": f"Unsupported media format: {ext}"}
    except ImportError:
        return {"error": "mutagen library not installed. Install with: pip install mutagen"}
    except Exception as e:
        return {"error": str(e)}

# ─── Router ─────────────────────────────────────────────────────────────────
def extract_metadata(path: str) -> Dict:
    """Universal metadata extraction router."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "extract_metadata")
    
    if not p.is_file():
        return {"error": f"File not found: {path}"}
    
    ext = p.suffix.lower()
    result = {"path": str(p), "filename": p.name, "extension": ext}
    
    # Audio formats
    if ext in ('.mp3', '.flac', '.ogg', '.m4a', '.wav', '.wma', '.aac'):
        result["type"] = "audio"
        result.update(_extract_audio_metadata_mutagen(p))
    
    # Image formats
    elif ext in ('.jpg', '.jpeg', '.png', '.tiff', '.webp', '.bmp', '.gif'):
        result["type"] = "image"
        result.update(_extract_exif(p))
    
    # Video formats
    elif ext in ('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'):
        result["type"] = "video"
        result.update(_extract_video(p))
    else:
        result["type"] = "unknown"
        result["supported_formats"] = {
            "audio": [".mp3", ".flac", ".ogg", ".m4a", ".wav"],
            "image": [".jpg", ".jpeg", ".png", ".webp"],
            "video": [".mp4", ".mkv", ".avi", ".mov"]
        }
    
    # Add category hints
    if result.get("type") == "video":
        height = result.get("video", {}).get("height", 0)
        if height >= 2160:
            result["category_hint"] = "4K-Video"
        elif height >= 1440:
            result["category_hint"] = "2K-Video"
        elif height >= 1080:
            result["category_hint"] = "Full-HD-Video"
        elif height >= 720:
            result["category_hint"] = "HD-Video"
        else:
            result["category_hint"] = "SD-Video"
    
    elif result.get("type") == "audio":
        genre = result.get("tags", {}).get("TCON", result.get("tags", {}).get("genre", ""))
        if genre:
            result["category_hint"] = f"Music-{genre}"
    
    conversation_memory.add(
        op="extract_metadata",
        paths={"path": str(p)},
        status="success", dialog=dialog_ctx.get(),
        context=f"Extracted {result['type']} metadata from {p.name}"
    )
    
    return result

def batch_extract_metadata(path: str, recursive: bool = False,
                           max_files: int = 100) -> Dict:
    """Batch metadata extraction from directory."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "batch_extract_metadata")
    
    if not p.is_dir():
        return {"error": f"Path is not a directory: {path}"}
    
    results = []
    count = 0
    errors = 0
    
    supported_exts = {
        '.jpg', '.jpeg', '.png', '.tiff', '.webp',
        '.mp3', '.flac', '.ogg', '.m4a', '.wav',
        '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm'
    }
    
    iterator = p.rglob("*") if recursive else p.iterdir()
    
    for item in iterator:
        if item.is_file() and item.suffix.lower() in supported_exts:
            try:
                results.append(extract_metadata(str(item)))
                count += 1
                if count >= max_files:
                    break
            except Exception as e:
                results.append({"path": str(item), "error": str(e)})
                errors += 1
    
    summary = {
        "images": len([r for r in results if r.get("type") == "image"]),
        "audio": len([r for r in results if r.get("type") == "audio"]),
        "video": len([r for r in results if r.get("type") == "video"]),
        "errors": errors
    }
    
    return {
        "path": str(p),
        "scanned": count,
        "results": results,
        "summary": summary
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-media", "3.1")

server.register_tool("extract_metadata", {
    "description": "Extract comprehensive metadata from audio, video, or image files",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    }
}, lambda **kw: extract_metadata(kw["path"]))

server.register_tool("batch_extract_metadata", {
    "description": "Batch metadata extraction from directory",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
            "max_files": {"type": "integer", "default": 100}
        },
        "required": ["path"]
    }
}, lambda **kw: batch_extract_metadata(
    kw["path"], kw.get("recursive", False), kw.get("max_files", 100)
))

server.register_tool("generate_thumbnail", {
    "description": "Generate thumbnail for image or video file",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "output_path": {"type": "string"},
            "size": {
                "type": "array",
                "items": {"type": "integer"},
                "default": [256, 256]
            },
            "quality": {"type": "integer", "default": 85}
        },
        "required": ["source_path", "output_path"]
    }
}, lambda **kw: generate_thumbnail(
    kw["source_path"], kw["output_path"],
    tuple(kw.get("size", [256, 256])), kw.get("quality", 85)
))

server.register_tool("extract_text_from_image", {
    "description": "Extract text from image using OCR (supports multiple languages)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "lang": {"type": "string", "default": "eng+rus"}
        },
        "required": ["image_path"]
    }
}, lambda **kw: extract_text_from_image(kw["image_path"], kw.get("lang", "eng+rus")))

server.register_tool("get_media_duration", {
    "description": "Get duration for audio or video file",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    }
}, lambda **kw: get_media_duration(kw["path"]))

if __name__ == "__main__":
    server.run()