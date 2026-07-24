"""
Video Trimming Service

Uses FFmpeg to trim video recordings and get video metadata.
"""

import os
import subprocess
import tempfile
import logging
import json
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoTrimmerService:
    """Service for trimming video files using FFmpeg."""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or tempfile.mkdtemp(prefix="video_trim_")
        os.makedirs(self.output_dir, exist_ok=True)

    def get_video_duration(self, video_path: str) -> Optional[float]:
        """
        Get the duration of a video file in seconds.

        Args:
            video_path: Path to the video file

        Returns:
            Duration in seconds or None on error
        """
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                video_path
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                logger.error(f"[TRIM] ffprobe error: {result.stderr}")
                return None

            data = json.loads(result.stdout)

            # Try to get duration from format
            if 'format' in data and 'duration' in data['format']:
                duration = float(data['format']['duration'])
                logger.info(f"[TRIM] Video duration: {duration:.2f} seconds")
                return duration

            # Fall back to stream duration
            for stream in data.get('streams', []):
                if 'duration' in stream:
                    duration = float(stream['duration'])
                    logger.info(f"[TRIM] Video duration (from stream): {duration:.2f} seconds")
                    return duration

            logger.warning("[TRIM] Could not determine video duration")
            return None

        except subprocess.TimeoutExpired:
            logger.error("[TRIM] ffprobe timed out")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"[TRIM] Error parsing ffprobe output: {e}")
            return None
        except Exception as e:
            logger.error(f"[TRIM] Error getting video duration: {e}")
            return None

    def get_video_info(self, video_path: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a video file.

        Returns:
            {
                "duration": float,
                "width": int,
                "height": int,
                "fps": float,
                "codec": str,
                "bitrate": int,
                "size_bytes": int
            }
        """
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                video_path
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return None

            data = json.loads(result.stdout)
            info = {
                "duration": 0,
                "width": 0,
                "height": 0,
                "fps": 0,
                "codec": "",
                "bitrate": 0,
                "size_bytes": os.path.getsize(video_path)
            }

            # Get format info
            if 'format' in data:
                fmt = data['format']
                info['duration'] = float(fmt.get('duration', 0))
                info['bitrate'] = int(fmt.get('bit_rate', 0))

            # Get video stream info
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    info['width'] = stream.get('width', 0)
                    info['height'] = stream.get('height', 0)
                    info['codec'] = stream.get('codec_name', '')

                    # Calculate FPS from frame rate
                    fps_str = stream.get('avg_frame_rate', '0/1')
                    if '/' in fps_str:
                        num, den = fps_str.split('/')
                        if int(den) > 0:
                            info['fps'] = float(num) / float(den)
                    break

            return info

        except Exception as e:
            logger.error(f"[TRIM] Error getting video info: {e}")
            return None

    def trim_video(
        self,
        video_path: str,
        start_time: float,
        end_time: float,
        output_filename: Optional[str] = None,
        progress_callback=None
    ) -> Optional[str]:
        """
        Trim a video file between start_time and end_time.

        Args:
            video_path: Path to the source video
            start_time: Start time in seconds
            end_time: End time in seconds
            output_filename: Optional output filename (default: trimmed_<original>)
            progress_callback: Optional callback(progress_percent)

        Returns:
            Path to the trimmed video or None on error
        """
        try:
            if start_time < 0:
                start_time = 0

            duration = end_time - start_time
            if duration <= 0:
                logger.error("[TRIM] Invalid trim duration")
                return None

            # Generate output path
            if output_filename:
                output_path = os.path.join(self.output_dir, output_filename)
            else:
                base_name = Path(video_path).stem
                output_path = os.path.join(self.output_dir, f"trimmed_{base_name}.mp4")

            logger.info(f"[TRIM] Trimming video from {start_time:.2f}s to {end_time:.2f}s ({duration:.2f}s)")

            # Use FFmpeg to trim
            # -ss before -i for fast seeking, then accurate duration with -t
            cmd = [
                'ffmpeg',
                '-y',  # Overwrite output
                '-ss', str(start_time),  # Seek to start time (fast)
                '-i', video_path,
                '-t', str(duration),  # Duration to capture
                '-c', 'copy',  # Copy codecs (fast, no re-encoding)
                '-avoid_negative_ts', 'make_zero',
                output_path
            ]

            # For accurate trimming (slower but precise), we'd use:
            # '-c:v', 'libx264', '-c:a', 'aac' instead of '-c', 'copy'

            logger.info(f"[TRIM] Running: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Monitor progress from FFmpeg stderr
            stderr_output = []
            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                stderr_output.append(line)

                # Parse FFmpeg progress (time=HH:MM:SS.ms)
                if 'time=' in line and progress_callback:
                    try:
                        import re
                        match = re.search(r'time=(\d+):(\d+):(\d+)\.(\d+)', line)
                        if match:
                            h, m, s, ms = map(int, match.groups())
                            current_time = h * 3600 + m * 60 + s + ms / 100
                            progress = min(100, (current_time / duration) * 100)
                            progress_callback(progress)
                    except Exception:
                        pass

            return_code = process.wait()

            if return_code != 0:
                stderr_text = ''.join(stderr_output)
                logger.error(f"[TRIM] FFmpeg failed with code {return_code}: {stderr_text}")
                return None

            if not os.path.exists(output_path):
                logger.error("[TRIM] Output file not created")
                return None

            output_size = os.path.getsize(output_path)
            logger.info(f"[TRIM] Trimmed video saved: {output_path} ({output_size} bytes)")

            return output_path

        except Exception as e:
            logger.error(f"[TRIM] Error trimming video: {e}")
            return None

    def trim_video_accurate(
        self,
        video_path: str,
        start_time: float,
        end_time: float,
        output_filename: Optional[str] = None,
        progress_callback=None
    ) -> Optional[str]:
        """
        Trim a video with frame-accurate cutting (slower, re-encodes).

        Use this when precise start/end times are required.
        """
        try:
            if start_time < 0:
                start_time = 0

            duration = end_time - start_time
            if duration <= 0:
                logger.error("[TRIM] Invalid trim duration")
                return None

            if output_filename:
                output_path = os.path.join(self.output_dir, output_filename)
            else:
                base_name = Path(video_path).stem
                output_path = os.path.join(self.output_dir, f"trimmed_{base_name}.mp4")

            logger.info(f"[TRIM] Accurate trim from {start_time:.2f}s to {end_time:.2f}s")

            # Accurate trimming with re-encoding
            cmd = [
                'ffmpeg',
                '-y',
                '-i', video_path,
                '-ss', str(start_time),  # After -i for accurate seeking
                '-t', str(duration),
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                output_path
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            stderr_output = []
            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                stderr_output.append(line)

                if 'time=' in line and progress_callback:
                    try:
                        import re
                        match = re.search(r'time=(\d+):(\d+):(\d+)\.(\d+)', line)
                        if match:
                            h, m, s, ms = map(int, match.groups())
                            current_time = h * 3600 + m * 60 + s + ms / 100
                            progress = min(100, (current_time / duration) * 100)
                            progress_callback(progress)
                    except Exception:
                        pass

            return_code = process.wait()

            if return_code != 0:
                stderr_text = ''.join(stderr_output)
                logger.error(f"[TRIM] FFmpeg failed: {stderr_text}")
                return None

            if not os.path.exists(output_path):
                return None

            return output_path

        except Exception as e:
            logger.error(f"[TRIM] Error in accurate trim: {e}")
            return None

    def trim_from_source(
        self,
        source: str,
        start_time: float,
        end_time: Optional[float] = None,
        output_filename: Optional[str] = None,
        progress_callback=None
    ) -> Optional[str]:
        """
        Trim straight from a URL (or path) without staging the whole file.

        ffmpeg reads the source over HTTP and, because -ss comes before -i,
        seeks with a range request instead of pulling everything from byte
        zero. Only the trimmed result touches disk — so publishing a 1.8 GB
        recording no longer needs ~4 GB free, which is what made the old
        download-then-trim path fail on small disks.

        Falls back to download-then-trim if the streaming attempt fails
        (some URLs don't support range requests).

        Args:
            source: HTTP(S) URL or local path
            start_time: seconds from the start of the recording
            end_time: seconds; None means "to the end"
            output_filename: name within output_dir
            progress_callback: called with 0-100

        Returns:
            Path to the trimmed file, or None on error.
        """
        output_path = os.path.join(
            self.output_dir, output_filename or "trimmed.mp4"
        )
        start_time = max(0.0, start_time)
        duration = (end_time - start_time) if end_time is not None else None
        if duration is not None and duration <= 0:
            logger.error("[TRIM] Invalid trim duration")
            return None

        cmd = [
            'ffmpeg', '-y',
            # Let ffmpeg follow Zoom's redirect to the CDN.
            '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '30',
            '-ss', str(start_time),
            '-i', source,
        ]
        if duration is not None:
            cmd += ['-t', str(duration)]
        cmd += [
            '-c', 'copy',
            '-movflags', '+faststart',
            '-avoid_negative_ts', 'make_zero',
            output_path,
        ]

        logger.info(
            f"[TRIM] Streaming trim from source, "
            f"{start_time:.1f}s for {duration if duration else 'rest'}s"
        )

        if self._run_ffmpeg(cmd, duration, progress_callback):
            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size > 0:
                logger.info(f"[TRIM] Streaming trim succeeded: {size} bytes")
                return output_path

        # Streaming didn't work — fall back to the old, disk-hungry path.
        if not str(source).lower().startswith("http"):
            return None

        logger.warning("[TRIM] Streaming trim failed; falling back to full download")
        try:
            import requests
            local = os.path.join(self.output_dir, "source.mp4")
            with requests.get(source, stream=True, timeout=3600) as r:
                r.raise_for_status()
                with open(local, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            logger.error(f"[TRIM] Fallback download failed: {e}")
            return None

        real_duration = self.get_video_duration(local)
        end = end_time if end_time is not None else (real_duration or 0)
        result = self.trim_video(
            local, start_time, end,
            output_filename=os.path.basename(output_path),
            progress_callback=progress_callback,
        )
        try:
            os.remove(local)
        except OSError:
            pass
        return result

    def _run_ffmpeg(self, cmd, duration: Optional[float], progress_callback) -> bool:
        """Run ffmpeg, forwarding progress. Returns True on exit code 0."""
        import re as _re
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        except FileNotFoundError:
            logger.error("[TRIM] ffmpeg not installed")
            return False

        tail = []
        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            if duration and progress_callback and 'time=' in line:
                match = _re.search(r'time=(\d+):(\d+):(\d+)\.(\d+)', line)
                if match:
                    h, m, s, cs = map(int, match.groups())
                    current = h * 3600 + m * 60 + s + cs / 100
                    try:
                        progress_callback(min(100, (current / duration) * 100))
                    except Exception:
                        pass

        code = process.wait()
        if code != 0:
            logger.error(f"[TRIM] ffmpeg exited {code}: {''.join(tail)[-800:]}")
        return code == 0

    def format_time(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"

    def parse_time(self, time_str: str) -> Optional[float]:
        """
        Parse a time string (HH:MM:SS or MM:SS or SS) to seconds.

        Args:
            time_str: Time in format "HH:MM:SS", "MM:SS", or seconds as string

        Returns:
            Time in seconds or None on parse error
        """
        try:
            time_str = time_str.strip()

            # Try as float first (seconds)
            try:
                return float(time_str)
            except ValueError:
                pass

            # Split by colon
            parts = time_str.split(':')

            if len(parts) == 3:
                # HH:MM:SS
                h, m, s = map(float, parts)
                return h * 3600 + m * 60 + s
            elif len(parts) == 2:
                # MM:SS
                m, s = map(float, parts)
                return m * 60 + s
            elif len(parts) == 1:
                # Just seconds
                return float(parts[0])
            else:
                return None

        except Exception as e:
            logger.error(f"[TRIM] Error parsing time '{time_str}': {e}")
            return None

    def cleanup(self):
        """Clean up temporary files."""
        try:
            import shutil
            if self.output_dir and os.path.exists(self.output_dir):
                shutil.rmtree(self.output_dir)
                logger.info(f"[TRIM] Cleaned up: {self.output_dir}")
        except Exception as e:
            logger.error(f"[TRIM] Error cleaning up: {e}")
