#!/usr/bin/env python3
"""
Video Silence Cutter - Desktop Application
Automatically removes silence from videos using auto-editor.
"""

import sys
import os
import subprocess
import json
import tempfile
import shutil
import time
import select
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QProgressBar,
    QGroupBox, QSpinBox, QCheckBox, QMessageBox, QFrame, QComboBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon


@dataclass
class VideoInfo:
    """Information about a video file."""
    path: str
    codec: str
    width: int
    height: int
    fps: float
    duration: float
    bitrate: int  # in kbps
    audio_codec: str
    is_variable_framerate: bool
    
    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"
    
    @property
    def duration_str(self) -> str:
        hours = int(self.duration // 3600)
        minutes = int((self.duration % 3600) // 60)
        seconds = int(self.duration % 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


def analyze_video(path: str) -> Optional[VideoInfo]:
    """Analyze video file using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        
        data = json.loads(result.stdout)
        
        # Find video stream
        video_stream = None
        audio_stream = None
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video' and not video_stream:
                video_stream = stream
            elif stream.get('codec_type') == 'audio' and not audio_stream:
                audio_stream = stream
        
        if not video_stream:
            return None
        
        # Parse frame rate
        fps_str = video_stream.get('r_frame_rate', '30/1')
        if '/' in fps_str:
            num, den = map(int, fps_str.split('/'))
            fps = num / den if den else 30.0
        else:
            fps = float(fps_str)
        
        # Check for variable frame rate
        avg_fps_str = video_stream.get('avg_frame_rate', fps_str)
        if '/' in avg_fps_str:
            num, den = map(int, avg_fps_str.split('/'))
            avg_fps = num / den if den else fps
        else:
            avg_fps = float(avg_fps_str)
        is_vfr = abs(fps - avg_fps) > 2.0  # Only flag significant VFR
        
        # Get format info
        format_info = data.get('format', {})
        duration = float(format_info.get('duration', 0))
        bitrate = int(format_info.get('bit_rate', 0)) // 1000  # Convert to kbps
        
        return VideoInfo(
            path=path,
            codec=video_stream.get('codec_name', 'unknown'),
            width=int(video_stream.get('width', 0)),
            height=int(video_stream.get('height', 0)),
            fps=fps,
            duration=duration,
            bitrate=bitrate,
            audio_codec=audio_stream.get('codec_name', 'unknown') if audio_stream else 'none',
            is_variable_framerate=is_vfr
        )
    except Exception as e:
        print(f"Error analyzing video: {e}")
        return None


def needs_preprocessing(info: VideoInfo) -> list[str]:
    """Check if video needs preprocessing before auto-editor."""
    issues = []
    
    # Variable frame rate is problematic (but only significant VFR)
    if info.is_variable_framerate:
        issues.append("Variable frame rate detected (common in phone recordings)")
    
    # Some codecs can cause issues - only flag truly problematic ones
    problematic_codecs = ['av1', 'mpeg2video', 'mpeg1video', 'wmv3', 'theora']
    if info.codec.lower() in problematic_codecs:
        issues.append(f"Codec '{info.codec}' may cause compatibility issues")
    
    return issues


def get_target_crf(original_bitrate_kbps: int) -> int:
    """Determine CRF based on source quality to preserve it."""
    if original_bitrate_kbps > 20000:  # 4K/High quality
        return 18
    elif original_bitrate_kbps > 8000:  # 1080p high
        return 20
    elif original_bitrate_kbps > 4000:  # 720p-1080p
        return 22
    else:  # Lower quality source
        return 23


# Speed presets for encoding
SPEED_PRESETS = {
    'Fastest': ('ultrafast', 'p1'),   # (libx264 preset, nvenc preset)
    'Fast': ('veryfast', 'p2'),
    'Balanced': ('fast', 'p4'),
    'Quality': ('medium', 'p5'),
    'Best Quality': ('slow', 'p7'),
}


def detect_hardware_encoders() -> dict:
    """Detect available hardware encoders by querying FFmpeg."""
    encoders = {'available': [], 'preferred': None}
    
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        
        # Check for various hardware encoders
        if 'h264_nvenc' in output:
            encoders['available'].append(('h264_nvenc', 'NVIDIA NVENC'))
        if 'h264_vaapi' in output:
            encoders['available'].append(('h264_vaapi', 'AMD/Intel VAAPI'))
        if 'h264_qsv' in output:
            encoders['available'].append(('h264_qsv', 'Intel QuickSync'))
        if 'h264_amf' in output:
            encoders['available'].append(('h264_amf', 'AMD AMF'))
        if 'h264_videotoolbox' in output:
            encoders['available'].append(('h264_videotoolbox', 'Apple VideoToolbox'))
    except Exception as e:
        print(f"Error detecting hardware encoders: {e}")
    
    if encoders['available']:
        encoders['preferred'] = encoders['available'][0][0]
    
    return encoders


class ProcessingThread(QThread):
    """Background thread for video processing."""
    progress = pyqtSignal(int, str)  # percentage, status message
    finished = pyqtSignal(bool, str)  # success, message
    
    def __init__(self, input_path: str, output_path: str, options: dict):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.options = options
        self._cancelled = False
        self._active_process = None  # Track subprocess for cancellation
    
    def cancel(self):
        """Request cancellation and terminate any active subprocess."""
        self._cancelled = True
        self._terminate_active_process()
    
    def _terminate_active_process(self):
        """Forcefully terminate the active subprocess if running."""
        proc = self._active_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    
    def _run_subprocess_cancellable(self, cmd, capture_stderr=False):
        """Run a subprocess with cancellation support and timeout.
        
        Returns (returncode, stderr_bytes) if capture_stderr, else (returncode, None).
        """
        stderr_pipe = subprocess.PIPE if capture_stderr else subprocess.DEVNULL
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=stderr_pipe
        )
        self._active_process = process
        
        try:
            # Poll with cancellation checks instead of blocking communicate()
            stderr_chunks = []
            while process.poll() is None:
                if self._cancelled:
                    self._terminate_active_process()
                    return (-1, b'Cancelled')
                time.sleep(0.5)
            
            # Process finished — read remaining stderr
            if capture_stderr:
                remaining = process.stderr.read()
                if remaining:
                    stderr_chunks.append(remaining)
            
            stderr_data = b''.join(stderr_chunks) if capture_stderr else None
            return (process.returncode, stderr_data)
        finally:
            self._active_process = None
    
    def run(self):
        temp_file = None
        try:
            temp_file = self._process()
        except Exception as e:
            self._cleanup(temp_file)
            self.finished.emit(False, f"Error: {str(e)}")
    
    def _process(self) -> Optional[str]:
        """Main processing pipeline. Returns temp_file path for cleanup."""
        if self._cancelled:
            return None
        
        # Step 1: Analyze video
        self.progress.emit(5, "Analyzing video...")
        info = analyze_video(self.input_path)
        if not info:
            self.finished.emit(False, "Failed to analyze video file")
            return None
        
        working_file = self.input_path
        temp_file = None
        
        # Step 2: Preprocess if auto-fix is enabled
        if self.options.get('auto_fix', True):
            self.progress.emit(10, "Preprocessing video for compatibility...")
            
            temp_dir = tempfile.mkdtemp(prefix='vsc_')
            temp_file = os.path.join(temp_dir, 'preprocessed.mp4')
            
            # Get encoding options
            preserve_quality = self.options.get('preserve_quality', False)
            speed_preset = self.options.get('speed_preset', 'Balanced')
            use_hw_encoder = self.options.get('use_hw_encoder', False)
            hw_encoder = self.options.get('hw_encoder', None)
            
            # Get preset names
            libx264_preset, nvenc_preset = SPEED_PRESETS.get(speed_preset, ('fast', 'p4'))
            
            # Calculate target bitrate
            if preserve_quality:
                target_bitrate = int(info.bitrate * 1.1)  # 10% headroom
            else:
                target_bitrate = int(info.bitrate * 0.8)  # Reasonable compression
            
            def build_ffmpeg_cmd(use_hw: bool) -> list:
                """Build FFmpeg command with either HW or SW encoder."""
                cmd = ['ffmpeg', '-y', '-threads', '0', '-i', self.input_path]
                
                if use_hw and hw_encoder:
                    # Hardware encoding
                    cmd.extend(['-c:v', hw_encoder])
                    if 'nvenc' in hw_encoder:
                        cmd.extend(['-preset', nvenc_preset, '-b:v', f'{target_bitrate}k'])
                    elif 'vaapi' in hw_encoder:
                        cmd.extend(['-b:v', f'{target_bitrate}k'])
                    elif 'qsv' in hw_encoder:
                        cmd.extend(['-preset', 'medium', '-b:v', f'{target_bitrate}k'])
                    else:
                        cmd.extend(['-b:v', f'{target_bitrate}k'])
                else:
                    # Software encoding (libx264)
                    cmd.extend(['-c:v', 'libx264', '-preset', libx264_preset, '-threads', '0'])
                    if preserve_quality:
                        cmd.extend(['-b:v', f'{target_bitrate}k'])
                    else:
                        crf = get_target_crf(info.bitrate)
                        cmd.extend(['-crf', str(crf)])
                
                # Common settings
                cmd.extend([
                    '-c:a', 'aac',
                    '-colorspace', 'bt709',
                    '-color_primaries', 'bt709',
                    '-color_trc', 'bt709',
                    '-map_metadata', '-1',
                    '-pix_fmt', 'yuv420p',
                    temp_file
                ])
                return cmd
            
            # Try hardware encoding first, fall back to software if it fails
            hw_success = False
            if use_hw_encoder and hw_encoder:
                self.progress.emit(12, f"Trying GPU encoding ({hw_encoder})...")
                cmd = build_ffmpeg_cmd(use_hw=True)
                returncode, _ = self._run_subprocess_cancellable(cmd, capture_stderr=True)
                
                if self._cancelled:
                    self._cleanup(temp_file)
                    return None
                
                if returncode == 0 and os.path.exists(temp_file):
                    hw_success = True
                    working_file = temp_file
                    self.progress.emit(30, "Preprocessing complete (GPU)")
                else:
                    # Clean up partial file from failed HW encoding
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                    self.progress.emit(15, "GPU encoding failed, using CPU...")
            
            if not hw_success:
                # Use software encoding
                cmd = build_ffmpeg_cmd(use_hw=False)
                returncode, stderr = self._run_subprocess_cancellable(cmd, capture_stderr=True)
                
                if self._cancelled:
                    self._cleanup(temp_file)
                    return None
                
                if returncode != 0:
                    error_msg = stderr.decode('utf-8', errors='replace')[:500] if stderr else 'Unknown error'
                    self.finished.emit(False, f"Preprocessing failed: {error_msg}")
                    self._cleanup(temp_file)
                    return None
                
                working_file = temp_file
                self.progress.emit(30, "Preprocessing complete")
        
        if self._cancelled:
            self._cleanup(temp_file)
            return None
        
        # Step 3: Run auto-editor (use system version for reliability)
        self.progress.emit(35, "Running auto-editor...")
        
        # Find auto-editor - prefer system installation
        auto_editor_cmd = 'auto-editor'
        for path in ['/usr/bin/auto-editor', '/usr/local/bin/auto-editor']:
            if os.path.exists(path):
                auto_editor_cmd = path
                break
        
        # Build auto-editor command
        cmd = [auto_editor_cmd, working_file, '-o', self.output_path]
        
        # Add options (only if different from defaults)
        threshold = self.options.get('threshold', 4)
        if threshold != 4:
            cmd.extend(['--edit', f'audio:threshold={threshold}%'])
        
        margin = self.options.get('margin', 6)
        if margin != 6:
            cmd.extend(['--frame-margin', str(margin)])
        
        # Silent speed — use modern --when-silent instead of deprecated --silent-speed
        silent_speed = self.options.get('silent_speed', 99999)
        if silent_speed != 99999:
            cmd.extend(['--when-silent', f'speed:{silent_speed}'])
        
        # Preserve quality - pass source bitrate to auto-editor
        preserve_quality = self.options.get('preserve_quality', False)
        if preserve_quality and info and info.bitrate > 0:
            # Pass source bitrate + 10% headroom to auto-editor
            target_bitrate = int(info.bitrate * 1.1)
            cmd.extend(['--video-bitrate', f'{target_bitrate}k'])
        
        # Run auto-editor
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1  # Line buffered
        )
        self._active_process = process
        
        # Monitor progress - capture output for error reporting
        start_time = time.time()
        estimated_duration = info.duration if info else 60
        last_status = "Analyzing audio..."
        output_lines = []  # Capture last lines for error reporting
        
        try:
            while True:
                if self._cancelled:
                    self._terminate_active_process()
                    self._cleanup(temp_file)
                    return None
                
                # Non-blocking read with timeout
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                
                if ready:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    
                    line = line.strip()
                    if line:
                        # Keep last 20 lines for error reporting
                        output_lines.append(line)
                        if len(output_lines) > 20:
                            output_lines.pop(0)
                        
                        # Try to parse progress from output
                        if 'rendering' in line.lower():
                            last_status = "Rendering edited video..."
                            self.progress.emit(60, last_status)
                        elif '%' in line:
                            try:
                                pct_str = line.split('%')[0].split()[-1]
                                pct = float(pct_str)
                                if 0 <= pct <= 100:
                                    self.progress.emit(40 + int(pct * 0.55), f"Processing: {int(pct)}%")
                            except (ValueError, IndexError):
                                pass
                else:
                    # No output ready, update based on time estimate
                    if process.poll() is not None:
                        break
                    elapsed = time.time() - start_time
                    time_pct = min(95, (elapsed / max(estimated_duration, 1)) * 100)
                    if time_pct > 40:
                        self.progress.emit(int(35 + time_pct * 0.6), last_status)
        finally:
            self._active_process = None
        
        if process.returncode != 0:
            # Include actual error output for diagnostics
            error_detail = '\n'.join(output_lines[-10:]) if output_lines else 'No output captured'
            self.finished.emit(
                False,
                f"auto-editor failed (exit code {process.returncode}).\n\n"
                f"Output:\n{error_detail}"
            )
            self._cleanup(temp_file)
            return None
        
        # Step 4: Cleanup
        self.progress.emit(98, "Cleaning up...")
        self._cleanup(temp_file)
        
        self.progress.emit(100, "Complete!")
        self.finished.emit(True, f"Video saved to:\n{self.output_path}")
        return None
    
    def _cleanup(self, temp_file: Optional[str]):
        """Clean up temporary files."""
        if temp_file:
            try:
                temp_dir = os.path.dirname(temp_file)
                if os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except OSError:
                pass


class AnalysisThread(QThread):
    """Background thread for video analysis to avoid blocking the UI."""
    analysis_done = pyqtSignal(object, str)  # VideoInfo (or None), file_path
    
    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
    
    def run(self):
        info = analyze_video(self.file_path)
        self.analysis_done.emit(info, self.file_path)


class MainWindow(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        self.processing_thread = None
        self._analysis_thread = None
        self._process_start_time = None
        # Detect available hardware encoders on startup
        self.hw_encoders = detect_hardware_encoders()
        self.setup_ui()
        self.apply_style()
        
        # Enable drag-and-drop
        self.setAcceptDrops(True)
    
    def setup_ui(self):
        self.setWindowTitle("Video Silence Cutter")
        self.setMinimumSize(620, 780)
        
        # Set window icon
        icon_path = Path(__file__).parent / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # ─── Header ───
        header_container = QWidget()
        header_container.setObjectName("headerContainer")
        header_layout = QVBoxLayout(header_container)
        header_layout.setContentsMargins(24, 20, 24, 16)
        header_layout.setSpacing(4)
        
        header = QLabel("Video Silence Cutter")
        header.setObjectName("headerTitle")
        header.setFont(QFont('', 20, QFont.Weight.Bold))
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(header)
        
        subtitle = QLabel("Automatically remove silent parts from your videos")
        subtitle.setObjectName("headerSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(subtitle)
        
        layout.addWidget(header_container)
        
        # ─── Scrollable content area ───
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(16)
        content_layout.setContentsMargins(24, 16, 24, 24)
        
        # ─── Drop Zone (Input) ───
        self.drop_zone = QWidget()
        self.drop_zone.setObjectName("dropZone")
        drop_layout = QVBoxLayout(self.drop_zone)
        drop_layout.setContentsMargins(20, 20, 20, 16)
        drop_layout.setSpacing(10)
        
        drop_label = QLabel("Drop a video file here")
        drop_label.setObjectName("dropLabel")
        drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_label.setFont(QFont('', 11))
        drop_layout.addWidget(drop_label)
        
        drop_hint = QLabel("or browse to select")
        drop_hint.setObjectName("dropHint")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(drop_hint)
        
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("No file selected")
        self.input_edit.textChanged.connect(self.on_input_changed)
        input_row.addWidget(self.input_edit)
        input_btn = QPushButton("Browse")
        input_btn.setObjectName("ghostBtn")
        input_btn.clicked.connect(self.browse_input)
        input_row.addWidget(input_btn)
        drop_layout.addLayout(input_row)
        
        content_layout.addWidget(self.drop_zone)
        
        # ─── Output ───
        output_card = QWidget()
        output_card.setObjectName("card")
        output_card_layout = QVBoxLayout(output_card)
        output_card_layout.setContentsMargins(16, 14, 16, 14)
        output_card_layout.setSpacing(8)
        
        output_title = QLabel("Output")
        output_title.setObjectName("sectionTitle")
        output_title.setFont(QFont('', 11, QFont.Weight.Bold))
        output_card_layout.addWidget(output_title)
        
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output file path...")
        output_row.addWidget(self.output_edit)
        output_btn = QPushButton("Browse")
        output_btn.setObjectName("ghostBtn")
        output_btn.clicked.connect(self.browse_output)
        output_row.addWidget(output_btn)
        output_card_layout.addLayout(output_row)
        
        content_layout.addWidget(output_card)
        
        # ─── Detection Options (always visible) ───
        options_card = QWidget()
        options_card.setObjectName("card")
        options_card_layout = QVBoxLayout(options_card)
        options_card_layout.setContentsMargins(16, 14, 16, 14)
        options_card_layout.setSpacing(10)
        
        options_title = QLabel("Detection")
        options_title.setObjectName("sectionTitle")
        options_title.setFont(QFont('', 11, QFont.Weight.Bold))
        options_card_layout.addWidget(options_title)
        
        # Threshold
        threshold_layout = QHBoxLayout()
        threshold_layout.setSpacing(10)
        threshold_label = QLabel("Silence Threshold")
        threshold_layout.addWidget(threshold_label)
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1, 20)
        self.threshold_spin.setValue(4)
        self.threshold_spin.setSuffix("%")
        self.threshold_spin.setToolTip("Audio level below which is considered silence (default: 4%)")
        threshold_layout.addWidget(self.threshold_spin)
        threshold_layout.addStretch()
        options_card_layout.addLayout(threshold_layout)
        
        threshold_hint = QLabel("How quiet audio must be to count as silence")
        threshold_hint.setObjectName("hintLabel")
        options_card_layout.addWidget(threshold_hint)
        
        # Margin
        margin_layout = QHBoxLayout()
        margin_layout.setSpacing(10)
        margin_label = QLabel("Frame Margin")
        margin_layout.addWidget(margin_label)
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 30)
        self.margin_spin.setValue(6)
        self.margin_spin.setSuffix(" frames")
        self.margin_spin.setToolTip("Buffer frames around loud sections for natural cuts")
        margin_layout.addWidget(self.margin_spin)
        margin_layout.addStretch()
        options_card_layout.addLayout(margin_layout)
        
        margin_hint = QLabel("Extra padding around speech for natural transitions")
        margin_hint.setObjectName("hintLabel")
        options_card_layout.addWidget(margin_hint)
        
        # Pre-process checkbox
        self.autofix_check = QCheckBox("Pre-process with ffmpeg (recommended)")
        self.autofix_check.setChecked(True)
        self.autofix_check.setToolTip("Converts video to standard H.264 format before processing")
        options_card_layout.addWidget(self.autofix_check)
        
        content_layout.addWidget(options_card)
        
        # ─── Advanced Options (collapsible) ───
        self.advanced_toggle = QPushButton("Advanced Options  \u25b8")
        self.advanced_toggle.setObjectName("advancedToggle")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        content_layout.addWidget(self.advanced_toggle)
        
        self.advanced_panel = QWidget()
        self.advanced_panel.setObjectName("card")
        self.advanced_panel.setVisible(False)
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(16, 14, 16, 14)
        advanced_layout.setSpacing(10)
        
        # Speed preset dropdown
        speed_layout = QHBoxLayout()
        speed_layout.setSpacing(10)
        speed_layout.addWidget(QLabel("Encoding Speed"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(['Fastest', 'Fast', 'Balanced', 'Quality', 'Best Quality'])
        self.speed_combo.setCurrentText('Balanced')
        self.speed_combo.setToolTip("Faster encoding = larger file size, slower = smaller file")
        speed_layout.addWidget(self.speed_combo)
        speed_layout.addStretch()
        advanced_layout.addLayout(speed_layout)
        
        # Hardware encoding checkbox
        self.hw_check = QCheckBox("Use GPU hardware encoding")
        if self.hw_encoders['available']:
            hw_names = ', '.join([name for _, name in self.hw_encoders['available']])
            self.hw_check.setToolTip(f"Available: {hw_names}")
            self.hw_check.setChecked(True)
        else:
            self.hw_check.setEnabled(False)
            self.hw_check.setToolTip("No GPU encoders detected")
        advanced_layout.addWidget(self.hw_check)
        
        # Preserve quality checkbox
        self.quality_check = QCheckBox("Preserve source quality (larger output)")
        self.quality_check.setChecked(True)
        self.quality_check.setToolTip("Match the original video bitrate instead of compressing")
        advanced_layout.addWidget(self.quality_check)
        
        content_layout.addWidget(self.advanced_panel)
        
        # ─── Video Info ───
        self.info_group = QWidget()
        self.info_group.setObjectName("infoCard")
        self.info_group.setVisible(False)
        info_layout = QVBoxLayout(self.info_group)
        info_layout.setContentsMargins(16, 14, 16, 14)
        info_layout.setSpacing(8)
        
        info_title = QLabel("Video Info")
        info_title.setObjectName("sectionTitle")
        info_title.setFont(QFont('', 11, QFont.Weight.Bold))
        info_layout.addWidget(info_title)
        
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        info_layout.addWidget(self.info_label)
        
        content_layout.addWidget(self.info_group)
        
        # ─── Action Buttons ───
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        self.process_btn = QPushButton("Process Video")
        self.process_btn.setObjectName("processBtn")
        self.process_btn.setMinimumHeight(48)
        self.process_btn.setFont(QFont('', 13, QFont.Weight.Bold))
        self.process_btn.clicked.connect(self.start_processing)
        self.process_btn.setEnabled(False)
        btn_layout.addWidget(self.process_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.setMinimumHeight(48)
        self.cancel_btn.setFont(QFont('', 13, QFont.Weight.Bold))
        self.cancel_btn.clicked.connect(self.cancel_processing)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setVisible(False)
        btn_layout.addWidget(self.cancel_btn)
        
        content_layout.addLayout(btn_layout)
        
        # ─── Progress ───
        self.progress_widget = QWidget()
        self.progress_widget.setObjectName("card")
        self.progress_widget.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_widget)
        progress_layout.setContentsMargins(16, 14, 16, 14)
        progress_layout.setSpacing(8)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(22)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        progress_layout.addWidget(self.progress_bar)
        
        status_row = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("elapsedLabel")
        status_row.addWidget(self.elapsed_label)
        progress_layout.addLayout(status_row)
        
        content_layout.addWidget(self.progress_widget)
        
        content_layout.addStretch()
        layout.addWidget(content)
    
    def _toggle_advanced(self, checked: bool):
        """Toggle visibility of advanced options panel."""
        self.advanced_panel.setVisible(checked)
        arrow = "\u25be" if checked else "\u25b8"
        self.advanced_toggle.setText(f"Advanced Options  {arrow}")
    
    def apply_style(self):
        """Apply modern dark theme styling."""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a2e;
            }
            
            /* ── Header ── */
            #headerContainer {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #16213e, stop:1 #0f3460);
                border-bottom: 2px solid #0078d4;
            }
            #headerTitle {
                color: #ffffff;
                font-size: 20pt;
            }
            #headerSubtitle {
                color: #8899aa;
                font-size: 10pt;
            }
            
            /* ── Cards ── */
            #card, #infoCard {
                background-color: #16213e;
                border: 1px solid #1a2744;
                border-radius: 10px;
            }
            #infoCard {
                border-left: 3px solid #0078d4;
            }
            #sectionTitle {
                color: #8899aa;
                font-size: 10pt;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            #hintLabel {
                color: #556677;
                font-size: 9pt;
                margin-top: -4px;
                margin-bottom: 2px;
            }
            
            /* ── Drop Zone ── */
            #dropZone {
                background-color: #16213e;
                border: 2px dashed #2a3a5e;
                border-radius: 12px;
            }
            #dropZone[dragOver="true"] {
                border-color: #0078d4;
                background-color: #1a2744;
            }
            #dropLabel {
                color: #aabbcc;
                font-size: 12pt;
            }
            #dropHint {
                color: #556677;
                font-size: 9pt;
            }
            
            /* ── Text Inputs ── */
            QWidget {
                color: #ccd6e0;
                font-size: 11pt;
            }
            QLineEdit {
                padding: 9px 14px;
                border: 1px solid #2a3a5e;
                border-radius: 8px;
                background-color: #0f1a30;
                color: #ccd6e0;
                font-size: 10pt;
            }
            QLineEdit:focus {
                border: 1px solid #0078d4;
            }
            
            /* ── Ghost Buttons (Browse) ── */
            #ghostBtn {
                padding: 9px 18px;
                border: 1px solid #2a3a5e;
                border-radius: 8px;
                background-color: transparent;
                color: #8899aa;
                font-weight: bold;
                font-size: 10pt;
            }
            #ghostBtn:hover {
                border-color: #0078d4;
                color: #0078d4;
                background-color: rgba(0, 120, 212, 0.08);
            }
            #ghostBtn:pressed {
                background-color: rgba(0, 120, 212, 0.15);
            }
            
            /* ── Process Button (Primary CTA) ── */
            #processBtn {
                border: none;
                border-radius: 10px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00b894, stop:1 #00a381);
                color: white;
                font-size: 13pt;
                font-weight: bold;
                padding: 12px;
            }
            #processBtn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d1a7, stop:1 #00b894);
            }
            #processBtn:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #009b7d, stop:1 #008a6e);
            }
            #processBtn:disabled {
                background: #1e2f4a;
                color: #445566;
            }
            
            /* ── Cancel Button ── */
            #cancelBtn {
                border: none;
                border-radius: 10px;
                background-color: #c0392b;
                color: white;
                font-size: 13pt;
                font-weight: bold;
                padding: 12px;
            }
            #cancelBtn:hover {
                background-color: #e74c3c;
            }
            #cancelBtn:pressed {
                background-color: #a93226;
            }
            
            /* ── Advanced Toggle ── */
            #advancedToggle {
                border: none;
                border-radius: 8px;
                background-color: transparent;
                color: #667788;
                font-size: 10pt;
                font-weight: bold;
                text-align: left;
                padding: 8px 4px;
            }
            #advancedToggle:hover {
                color: #0078d4;
            }
            #advancedToggle:checked {
                color: #0078d4;
            }
            
            /* ── SpinBox ── */
            QSpinBox {
                padding: 7px 12px;
                border: 1px solid #2a3a5e;
                border-radius: 8px;
                background-color: #0f1a30;
                color: #ccd6e0;
            }
            QSpinBox:focus {
                border-color: #0078d4;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                width: 20px;
                border: none;
                background: transparent;
            }
            
            /* ── ComboBox ── */
            QComboBox {
                padding: 7px 12px;
                border: 1px solid #2a3a5e;
                border-radius: 8px;
                background-color: #0f1a30;
                color: #ccd6e0;
                min-width: 130px;
            }
            QComboBox:hover, QComboBox:focus {
                border-color: #0078d4;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 10px;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #ccd6e0;
                border: 1px solid #2a3a5e;
                selection-background-color: #0078d4;
                selection-color: white;
                padding: 4px;
                border-radius: 6px;
            }
            
            /* ── CheckBox ── */
            QCheckBox {
                spacing: 10px;
                font-size: 10pt;
            }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border-radius: 5px;
                border: 2px solid #2a3a5e;
                background-color: #0f1a30;
            }
            QCheckBox::indicator:hover {
                border-color: #0078d4;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QCheckBox:disabled {
                color: #445566;
            }
            QCheckBox::indicator:disabled {
                border-color: #1e2f4a;
                background-color: #0f1a30;
            }
            
            /* ── ProgressBar ── */
            QProgressBar {
                border: none;
                border-radius: 6px;
                background-color: #0f1a30;
                text-align: center;
                color: #ffffff;
                font-size: 9pt;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #667eea, stop:1 #764ba2);
                border-radius: 6px;
            }
            
            /* ── Status Labels ── */
            #statusLabel {
                color: #8899aa;
                font-size: 10pt;
            }
            #elapsedLabel {
                color: #556677;
                font-size: 9pt;
            }
            
            /* ── Tooltip ── */
            QToolTip {
                background-color: #16213e;
                color: #ccd6e0;
                border: 1px solid #2a3a5e;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 10pt;
            }
        """)
    
    def browse_input(self):
        """Open file dialog for input video."""
        file, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", "",
            "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.wmv *.flv);;All Files (*)"
        )
        if file:
            self.input_edit.setText(file)
    
    def browse_output(self):
        """Open file dialog for output video."""
        default_name = ""
        if self.input_edit.text():
            input_path = Path(self.input_edit.text())
            default_name = str(input_path.parent / f"{input_path.stem}_cleaned.mp4")
        
        file, _ = QFileDialog.getSaveFileName(
            self, "Save Output File", default_name,
            "MP4 Video (*.mp4);;MKV Video (*.mkv);;All Files (*)"
        )
        if file:
            self.output_edit.setText(file)
    
    def on_input_changed(self, text):
        """Handle input file change — runs analysis in background."""
        if os.path.isfile(text):
            # Auto-generate output path
            input_path = Path(text)
            output_path = input_path.parent / f"{input_path.stem}_cleaned.mp4"
            self.output_edit.setText(str(output_path))
            
            # Show loading state
            self.info_label.setText("<i style='color: #667788;'>Analyzing video...</i>")
            self.info_group.setVisible(True)
            self.process_btn.setEnabled(False)
            
            # Run analysis in background thread
            self._analysis_thread = AnalysisThread(text)
            self._analysis_thread.analysis_done.connect(self._on_analysis_done)
            self._analysis_thread.start()
        else:
            self.info_group.setVisible(False)
            self.process_btn.setEnabled(False)
    
    def _on_analysis_done(self, info, file_path: str):
        """Handle background analysis completion."""
        if self.input_edit.text() != file_path:
            return
        
        if info:
            issues = needs_preprocessing(info)
            issue_html = ""
            if issues:
                issue_items = ''.join(f"<br><span style='color: #e67e22;'>\u26a0 {i}</span>" for i in issues)
                issue_html = issue_items
            
            self.info_label.setText(
                f"<table style='color: #8899aa; font-size: 10pt;' cellspacing='6'>"
                f"<tr><td style='color: #556677;'>Format</td>"
                f"<td style='color: #ccd6e0;'>{info.codec.upper()} &middot; {info.resolution} &middot; {info.fps:.1f}fps</td></tr>"
                f"<tr><td style='color: #556677;'>Duration</td>"
                f"<td style='color: #ccd6e0;'>{info.duration_str}</td></tr>"
                f"<tr><td style='color: #556677;'>Audio</td>"
                f"<td style='color: #ccd6e0;'>{info.audio_codec.upper()}</td></tr>"
                f"<tr><td style='color: #556677;'>Bitrate</td>"
                f"<td style='color: #ccd6e0;'>{info.bitrate:,} kbps</td></tr>"
                f"</table>{issue_html}"
            )
            self.info_group.setVisible(True)
        else:
            self.info_label.setText("<i style='color: #667788;'>Could not analyze video file.</i>")
        
        self.process_btn.setEnabled(True)
    
    def dragEnterEvent(self, event):
        """Accept drag events and highlight drop zone."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.setProperty("dragOver", True)
            self.drop_zone.style().unpolish(self.drop_zone)
            self.drop_zone.style().polish(self.drop_zone)
    
    def dragLeaveEvent(self, event):
        """Remove drop zone highlight."""
        self.drop_zone.setProperty("dragOver", False)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)
    
    def dropEvent(self, event):
        """Handle dropped files."""
        self.drop_zone.setProperty("dragOver", False)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)
        
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if os.path.isfile(file_path):
                self.input_edit.setText(file_path)
    
    def start_processing(self):
        """Start video processing."""
        input_path = self.input_edit.text()
        output_path = self.output_edit.text()
        
        if not os.path.isfile(input_path):
            QMessageBox.warning(self, "Error", "Please select a valid input file.")
            return
        
        if not output_path:
            QMessageBox.warning(self, "Error", "Please specify an output file.")
            return
        
        # Validate input != output
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            QMessageBox.warning(self, "Error", "Output file cannot be the same as input file.")
            return
        
        # Validate output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.isdir(output_dir):
            QMessageBox.warning(self, "Error", f"Output directory does not exist:\n{output_dir}")
            return
        
        if os.path.exists(output_path):
            reply = QMessageBox.question(
                self, "File Exists",
                f"Output file already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
        # Gather options
        options = {
            'threshold': self.threshold_spin.value(),
            'margin': self.margin_spin.value(),
            'auto_fix': self.autofix_check.isChecked(),
            'silent_speed': 99999,  # Cut completely
            'speed_preset': self.speed_combo.currentText(),
            'use_hw_encoder': self.hw_check.isChecked() and self.hw_check.isEnabled(),
            'hw_encoder': self.hw_encoders.get('preferred'),
            'preserve_quality': self.quality_check.isChecked(),
        }
        
        # Update UI
        self.process_btn.setEnabled(False)
        self.process_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setVisible(True)
        self.progress_widget.setVisible(True)
        self.progress_bar.setValue(0)
        self._process_start_time = time.time()
        self.elapsed_label.setText("")
        
        # Start processing thread
        self.processing_thread = ProcessingThread(input_path, output_path, options)
        self.processing_thread.progress.connect(self.on_progress)
        self.processing_thread.finished.connect(self.on_finished)
        self.processing_thread.start()
    
    def cancel_processing(self):
        """Cancel video processing."""
        if self.processing_thread and self.processing_thread.isRunning():
            self.processing_thread.cancel()
            self.status_label.setText("Cancelling...")
    
    def on_progress(self, percent: int, status: str):
        """Handle progress updates with elapsed time."""
        self.progress_bar.setValue(percent)
        self.status_label.setText(status)
        
        # Update elapsed time
        if self._process_start_time:
            elapsed = int(time.time() - self._process_start_time)
            mins, secs = divmod(elapsed, 60)
            self.elapsed_label.setText(f"Elapsed: {mins:02d}:{secs:02d}")
    
    def on_finished(self, success: bool, message: str):
        """Handle processing completion."""
        self.process_btn.setEnabled(True)
        self.process_btn.setVisible(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setVisible(False)
        self._process_start_time = None
        
        if success:
            self.progress_bar.setValue(100)
            self.status_label.setText("Complete!")
            self.status_label.setStyleSheet("color: #00b894; font-weight: bold;")
            QMessageBox.information(self, "Success", message)
            self.status_label.setStyleSheet("")  # Reset
        else:
            self.progress_bar.setValue(0)
            self.status_label.setText("Failed")
            self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            QMessageBox.critical(self, "Error", message)
            self.status_label.setStyleSheet("")  # Reset
    
    def closeEvent(self, event):
        """Clean up processing thread when window is closed."""
        if self.processing_thread and self.processing_thread.isRunning():
            self.processing_thread.cancel()
            self.processing_thread.wait(timeout=5000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Video Silence Cutter")
    
    # Set application-wide font
    font = QFont("Segoe UI, Ubuntu, Cantarell, sans-serif", 10)
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


