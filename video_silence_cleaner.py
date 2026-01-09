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
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QProgressBar,
    QGroupBox, QSpinBox, QDoubleSpinBox, QCheckBox, QRadioButton,
    QButtonGroup, QTextEdit, QMessageBox, QFrame, QStyle
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor


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


COMPATIBLE_CODECS = ['h264', 'avc', 'avc1']  # Codecs that work well with auto-editor


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
    
    def cancel(self):
        self._cancelled = True
    
    def run(self):
        try:
            self._process()
        except Exception as e:
            self.finished.emit(False, f"Error: {str(e)}")
    
    def _process(self):
        if self._cancelled:
            return
        
        # Step 1: Analyze video
        self.progress.emit(5, "Analyzing video...")
        info = analyze_video(self.input_path)
        if not info:
            self.finished.emit(False, "Failed to analyze video file")
            return
        
        working_file = self.input_path
        temp_file = None
        
        # Step 2: Preprocess if auto-fix is enabled
        # Always preprocess when enabled - some videos need it even if they look compatible
        if self.options.get('auto_fix', True):
            self.progress.emit(10, "Preprocessing video for compatibility...")
            
            temp_dir = tempfile.mkdtemp(prefix='vsc_')
            temp_file = os.path.join(temp_dir, 'preprocessed.mp4')
            
            crf = get_target_crf(info.bitrate)
            
            # Full preprocessing command - ensures maximum compatibility with auto-editor
            cmd = [
                'ffmpeg', '-y', '-i', self.input_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', str(crf),
                '-c:a', 'aac',
                '-colorspace', 'bt709',
                '-color_primaries', 'bt709',
                '-color_trc', 'bt709',
                '-map_metadata', '-1',
                '-pix_fmt', 'yuv420p',
                temp_file
            ]
            
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _, stderr = process.communicate()
            
            if process.returncode != 0:
                self.finished.emit(False, f"Preprocessing failed: {stderr.decode()[:500]}")
                return
            
            working_file = temp_file
            self.progress.emit(30, "Preprocessing complete")
        
        if self._cancelled:
            self._cleanup(temp_file)
            return
        
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
            cmd.extend(['--margin', f'{margin}f'])
        
        # Silent speed (99999 = cut completely, which is default)
        silent_speed = self.options.get('silent_speed', 99999)
        if silent_speed != 99999:
            cmd.extend(['--silent-speed', str(silent_speed)])
        
        # Run auto-editor
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1  # Line buffered
        )
        
        # Monitor progress - use time-based estimation if we can't parse output
        import time
        start_time = time.time()
        estimated_duration = info.duration if info else 60  # Estimate processing takes ~1x video duration
        last_status = "Analyzing audio..."
        
        while True:
            if self._cancelled:
                process.terminate()
                self._cleanup(temp_file)
                return
            
            # Non-blocking read with timeout
            import select
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            
            if ready:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                
                line = line.strip()
                if line:
                    # Try to parse machine-readable progress
                    if line.startswith('analyze:'):
                        last_status = "Analyzing audio for silence..."
                        self.progress.emit(40, last_status)
                    elif line.startswith('render:') or 'rendering' in line.lower():
                        last_status = "Rendering edited video..."
                        self.progress.emit(60, last_status)
                    elif '%' in line or line.replace('.', '').isdigit():
                        try:
                            # Try to extract percentage
                            pct_str = line.split('%')[0].split()[-1] if '%' in line else line
                            pct = float(pct_str)
                            if 0 <= pct <= 100:
                                self.progress.emit(40 + int(pct * 0.55), f"Processing: {int(pct)}%")
                        except:
                            pass
            else:
                # No output ready, update based on time estimate
                if process.poll() is not None:
                    break
                elapsed = time.time() - start_time
                # Estimate: analyzing takes ~30% of time, rendering ~70%
                time_pct = min(95, (elapsed / max(estimated_duration, 1)) * 100)
                if time_pct > 40:
                    self.progress.emit(int(35 + time_pct * 0.6), last_status)
        
        if process.returncode != 0:
            self.finished.emit(False, "auto-editor failed. Check that the video has audio.")
            self._cleanup(temp_file)
            return
        
        # Step 4: Cleanup
        self.progress.emit(98, "Cleaning up...")
        self._cleanup(temp_file)
        
        self.progress.emit(100, "Complete!")
        self.finished.emit(True, f"Video saved to:\n{self.output_path}")
    
    def _cleanup(self, temp_file: Optional[str]):
        if temp_file and os.path.exists(temp_file):
            try:
                temp_dir = os.path.dirname(temp_file)
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass


class MainWindow(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        self.processing_thread = None
        self.setup_ui()
        self.apply_style()
    
    def setup_ui(self):
        self.setWindowTitle("Video Silence Cutter")
        self.setMinimumSize(600, 700)
        
        # Set window icon
        icon_path = Path(__file__).parent / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Header
        header = QLabel("🎬 Video Silence Cutter")
        header.setFont(QFont('', 18, QFont.Weight.Bold))
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)
        
        subtitle = QLabel("Automatically remove silent parts from your videos")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)
        
        layout.addSpacing(10)
        
        # Input section
        input_group = QGroupBox("📥 Input Video")
        input_layout = QHBoxLayout(input_group)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Select a video file...")
        self.input_edit.textChanged.connect(self.on_input_changed)
        input_layout.addWidget(self.input_edit)
        input_btn = QPushButton("Browse...")
        input_btn.clicked.connect(self.browse_input)
        input_layout.addWidget(input_btn)
        layout.addWidget(input_group)
        
        # Output section
        output_group = QGroupBox("📤 Output File")
        output_layout = QHBoxLayout(output_group)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output file path...")
        output_layout.addWidget(self.output_edit)
        output_btn = QPushButton("Browse...")
        output_btn.clicked.connect(self.browse_output)
        output_layout.addWidget(output_btn)
        layout.addWidget(output_group)
        
        # Options section
        options_group = QGroupBox("⚙️ Options")
        options_layout = QVBoxLayout(options_group)
        
        # Threshold
        threshold_layout = QHBoxLayout()
        threshold_layout.addWidget(QLabel("Silence Threshold:"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1, 20)
        self.threshold_spin.setValue(4)
        self.threshold_spin.setSuffix("%")
        self.threshold_spin.setToolTip("Audio level below which is considered silence (default: 4%)")
        threshold_layout.addWidget(self.threshold_spin)
        threshold_layout.addWidget(QLabel("(lower = more sensitive)"))
        threshold_layout.addStretch()
        options_layout.addLayout(threshold_layout)
        
        # Margin
        margin_layout = QHBoxLayout()
        margin_layout.addWidget(QLabel("Frame Margin:"))
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 30)
        self.margin_spin.setValue(6)
        self.margin_spin.setSuffix(" frames")
        self.margin_spin.setToolTip("Buffer frames around loud sections for natural cuts")
        margin_layout.addWidget(self.margin_spin)
        margin_layout.addStretch()
        options_layout.addLayout(margin_layout)
        
        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #444;")
        options_layout.addWidget(line)
        
        # Auto-fix checkbox
        self.autofix_check = QCheckBox("Pre-process video with ffmpeg (recommended)")
        self.autofix_check.setChecked(True)
        self.autofix_check.setToolTip("Converts video to standard H.264 format before processing - fixes most compatibility issues")
        options_layout.addWidget(self.autofix_check)
        
        layout.addWidget(options_group)
        
        # Video info display
        self.info_group = QGroupBox("📊 Video Info")
        self.info_group.setVisible(False)
        info_layout = QVBoxLayout(self.info_group)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        info_layout.addWidget(self.info_label)
        layout.addWidget(self.info_group)
        
        # Process buttons
        btn_layout = QHBoxLayout()
        self.process_btn = QPushButton("▶️  Process Video")
        self.process_btn.setMinimumHeight(45)
        self.process_btn.setFont(QFont('', 12, QFont.Weight.Bold))
        self.process_btn.clicked.connect(self.start_processing)
        self.process_btn.setEnabled(False)
        btn_layout.addWidget(self.process_btn)
        
        self.cancel_btn = QPushButton("❌ Cancel")
        self.cancel_btn.setMinimumHeight(45)
        self.cancel_btn.clicked.connect(self.cancel_processing)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setVisible(False)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)
        
        # Progress section
        self.progress_group = QGroupBox("📊 Progress")
        self.progress_group.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_group)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(25)
        progress_layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_layout.addWidget(self.status_label)
        
        layout.addWidget(self.progress_group)
        
        layout.addStretch()
    
    def apply_style(self):
        """Apply dark theme styling."""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QWidget {
                color: #e0e0e0;
                font-size: 11pt;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #3c3c3c;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 10px;
                background-color: #252526;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 15px;
                padding: 0 8px;
            }
            QLineEdit {
                padding: 8px 12px;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                background-color: #2d2d2d;
                color: #e0e0e0;
            }
            QLineEdit:focus {
                border: 1px solid #0078d4;
            }
            QPushButton {
                padding: 8px 20px;
                border: none;
                border-radius: 6px;
                background-color: #0078d4;
                color: white;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1084d8;
            }
            QPushButton:pressed {
                background-color: #006cbd;
            }
            QPushButton:disabled {
                background-color: #3c3c3c;
                color: #808080;
            }
            QSpinBox, QDoubleSpinBox {
                padding: 6px 10px;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                background-color: #2d2d2d;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #3c3c3c;
                background-color: #2d2d2d;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QProgressBar {
                border: none;
                border-radius: 8px;
                background-color: #2d2d2d;
                text-align: center;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #667eea, stop:1 #764ba2);
                border-radius: 8px;
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
        # Default to same directory as input with _cleaned suffix
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
        """Handle input file change."""
        if os.path.isfile(text):
            # Auto-generate output path
            input_path = Path(text)
            output_path = input_path.parent / f"{input_path.stem}_cleaned.mp4"
            self.output_edit.setText(str(output_path))
            
            # Analyze and show video info
            info = analyze_video(text)
            if info:
                issues = needs_preprocessing(info)
                issue_text = ""
                if issues:
                    issue_text = "\n⚠️ " + "\n⚠️ ".join(issues)
                
                self.info_label.setText(
                    f"<b>Format:</b> {info.codec.upper()} {info.resolution} @ {info.fps:.1f}fps<br>"
                    f"<b>Duration:</b> {info.duration_str}<br>"
                    f"<b>Audio:</b> {info.audio_codec.upper()}<br>"
                    f"<b>Bitrate:</b> {info.bitrate} kbps"
                    f"<span style='color: #ffa500;'>{issue_text.replace(chr(10), '<br>')}</span>"
                )
                self.info_group.setVisible(True)
            
            self.process_btn.setEnabled(True)
        else:
            self.info_group.setVisible(False)
            self.process_btn.setEnabled(False)
    
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
            'silent_speed': 99999  # Cut completely
        }
        
        # Update UI
        self.process_btn.setEnabled(False)
        self.process_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setVisible(True)
        self.progress_group.setVisible(True)
        self.progress_bar.setValue(0)
        
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
        """Handle progress updates."""
        self.progress_bar.setValue(percent)
        self.status_label.setText(status)
    
    def on_finished(self, success: bool, message: str):
        """Handle processing completion."""
        self.process_btn.setEnabled(True)
        self.process_btn.setVisible(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setVisible(False)
        
        if success:
            self.progress_bar.setValue(100)
            self.status_label.setText("✅ Complete!")
            QMessageBox.information(self, "Success", message)
        else:
            self.progress_bar.setValue(0)
            self.status_label.setText("❌ Failed")
            QMessageBox.critical(self, "Error", message)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Video Silence Cutter")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
