#!/bin/bash

# Build script for Video Silence Cutter
# Creates a standalone binary using PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
APP_NAME="video-silence-cutter"

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║        Video Silence Cutter - Build Script                ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Check if venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "❌ Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install PyInstaller if not present
echo "📦 Installing PyInstaller..."
pip install pyinstaller --quiet

# Build the binary
echo "🔨 Building standalone binary..."
pyinstaller \
    --onefile \
    --windowed \
    --name "$APP_NAME" \
    --icon "icon.png" \
    --add-data "icon.png:." \
    --hidden-import "PyQt6.QtSvg" \
    --hidden-import "PyQt6.QtSvgWidgets" \
    video_silence_cleaner.py

echo ""
echo "✅ Build complete!"
echo ""
echo "Binary location: $SCRIPT_DIR/dist/$APP_NAME"
echo ""
echo "To distribute:"
echo "  1. Copy dist/$APP_NAME to the target machine"
echo "  2. Make sure ffmpeg and auto-editor are installed:"
echo "     sudo apt install ffmpeg auto-editor"
echo "  3. Run: ./$APP_NAME"
echo ""
