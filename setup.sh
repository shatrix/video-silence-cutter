#!/bin/bash

# Video Silence Cutter - Setup Script
# This script installs all dependencies and sets up the application

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="video-silence-cleaner"
VENV_DIR="$SCRIPT_DIR/venv"
DESKTOP_FILE="$HOME/.local/share/applications/${APP_NAME}.desktop"

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║        Video Silence Cutter - Installation Script         ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Detect package manager
detect_package_manager() {
    if command -v apt &> /dev/null; then
        echo "apt"
    elif command -v dnf &> /dev/null; then
        echo "dnf"
    elif command -v pacman &> /dev/null; then
        echo "pacman"
    elif command -v zypper &> /dev/null; then
        echo "zypper"
    else
        echo "unknown"
    fi
}

PKG_MANAGER=$(detect_package_manager)
echo "📦 Detected package manager: $PKG_MANAGER"
echo ""

# Install system dependencies
install_system_deps() {
    echo "🔧 Installing system dependencies..."
    
    case $PKG_MANAGER in
        apt)
            sudo apt update
            sudo apt install -y ffmpeg python3 python3-pip python3-venv
            ;;
        dnf)
            sudo dnf install -y ffmpeg python3 python3-pip python3-virtualenv
            ;;
        pacman)
            sudo pacman -Sy --noconfirm ffmpeg python python-pip python-virtualenv
            ;;
        zypper)
            sudo zypper install -y ffmpeg python3 python3-pip python3-virtualenv
            ;;
        *)
            echo "❌ Unknown package manager. Please install manually:"
            echo "   - ffmpeg"
            echo "   - python3"
            echo "   - python3-pip"
            echo "   - python3-venv"
            exit 1
            ;;
    esac
    
    echo "✅ System dependencies installed"
}

# Check if ffmpeg is installed
check_ffmpeg() {
    if ! command -v ffmpeg &> /dev/null; then
        echo "❌ ffmpeg not found. Installing..."
        install_system_deps
    else
        echo "✅ ffmpeg found: $(ffmpeg -version 2>&1 | head -n1)"
    fi
}

# Check if auto-editor is installed (system package)
check_auto_editor() {
    if ! command -v auto-editor &> /dev/null; then
        echo "❌ auto-editor not found."
        echo ""
        echo "   Please install auto-editor using one of these methods:"
        echo ""
        case $PKG_MANAGER in
            apt)
                echo "   sudo apt install auto-editor"
                ;;
            dnf)
                echo "   sudo dnf install auto-editor"
                ;;
            pacman)
                echo "   sudo pacman -S auto-editor   # or from AUR"
                ;;
            *)
                echo "   pip install auto-editor"
                ;;
        esac
        echo ""
        echo "   Or install via pip: pip install auto-editor"
        echo ""
        exit 1
    else
        echo "✅ auto-editor found: $(auto-editor --version 2>&1 | head -n1 || echo 'installed')"
    fi
}

# Check if Python 3.9+ is installed
check_python() {
    if ! command -v python3 &> /dev/null; then
        echo "❌ Python 3 not found. Installing..."
        install_system_deps
    else
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        echo "✅ Python found: $PYTHON_VERSION"
        
        # Check version >= 3.9
        MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
        MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
        if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 9 ]); then
            echo "❌ Python 3.9+ required. Found $PYTHON_VERSION"
            exit 1
        fi
    fi
}

# Create virtual environment
setup_venv() {
    echo ""
    echo "🐍 Setting up Python virtual environment..."
    
    if [ -d "$VENV_DIR" ]; then
        echo "   Virtual environment already exists, updating..."
    else
        python3 -m venv "$VENV_DIR"
        echo "   Created virtual environment at $VENV_DIR"
    fi
    
    # Activate and install dependencies
    source "$VENV_DIR/bin/activate"
    
    echo "   Upgrading pip..."
    pip install --upgrade pip --quiet
    
    echo "   Installing Python packages..."
    pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
    
    echo "✅ Python environment ready"
}

# Create launcher script
create_launcher() {
    echo ""
    echo "🚀 Creating launcher script..."
    
    LAUNCHER="$SCRIPT_DIR/launch.sh"
    
    cat > "$LAUNCHER" << EOF
#!/bin/bash
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/venv/bin/activate"
python3 "\$SCRIPT_DIR/video_silence_cleaner.py" "\$@"
EOF
    
    chmod +x "$LAUNCHER"
    echo "✅ Launcher created: $LAUNCHER"
}

# Create desktop entry
create_desktop_entry() {
    echo ""
    echo "🖥️  Creating desktop entry..."
    
    mkdir -p "$HOME/.local/share/applications"
    
    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Video Silence Cutter
Comment=Remove silence from videos automatically
Exec=$SCRIPT_DIR/launch.sh
Icon=$SCRIPT_DIR/icon.png
Terminal=false
Categories=AudioVideo;Video;
StartupNotify=true
EOF
    
    # Update desktop database
    if command -v update-desktop-database &> /dev/null; then
        update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    fi
    
    echo "✅ Desktop entry created"
    echo "   The app should now appear in your application menu"
}

# Main installation flow
main() {
    echo "Step 1/6: Checking ffmpeg..."
    check_ffmpeg
    
    echo ""
    echo "Step 2/6: Checking auto-editor..."
    check_auto_editor
    
    echo ""
    echo "Step 3/6: Checking Python..."
    check_python
    
    echo ""
    echo "Step 4/6: Setting up Python environment..."
    setup_venv
    
    echo ""
    echo "Step 5/6: Creating launcher..."
    create_launcher
    
    echo ""
    echo "Step 6/6: Creating desktop entry..."
    create_desktop_entry
    
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║              ✅ Installation Complete!                    ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""
    echo "You can now:"
    echo "  • Run from terminal: $SCRIPT_DIR/launch.sh"
    echo "  • Find 'Video Silence Cutter' in your application menu"
    echo ""
}

main
