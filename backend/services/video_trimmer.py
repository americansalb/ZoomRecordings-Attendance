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
