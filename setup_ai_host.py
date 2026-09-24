"""
AI Host Setup Script
Installs dependencies and enables AI radio host features.
"""

import subprocess
import sys
from pathlib import Path

def check_python_version():
    """Ensure Python 3.10+ is installed."""
    if sys.version_info < (3, 10):
        print("❌ Python 3.10 or higher required")
        sys.exit(1)
    print(f"✅ Python {sys.version}")

def install_dependencies():
    """Install AI-related packages."""
    packages = [
        "transformers",
        "torch",
        "accelerate",
        "sentencepiece",
    ]
    
    print("\n📦 Installing AI dependencies...")
    for pkg in packages:
        print(f"  Installing {pkg}...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q", pkg],
                stdout=subprocess.DEVNULL,
            )
            print(f"  ✅ {pkg}")
        except subprocess.CalledProcessError as e:
            print(f"  ❌ Failed to install {pkg}: {e}")
            return False
    return True

def enable_ai_in_database():
    """Enable AI host in the database settings."""
    import sqlite3
    
    db_path = Path(__file__).parent / "data" / "cleanroom.db"
    if not db_path.exists():
        print(f"\n⚠️  Database not found at {db_path}")
        print("   AI will be enabled when you first run the server")
        return
    
    conn = sqlite3.connect(str(db_path))
    try:
        # Enable AI host
        conn.execute("""
            INSERT INTO station_settings (station_id, key, value)
            VALUES (1, 'ai_host_enabled', 'true')
            ON CONFLICT(station_id, key) DO UPDATE SET value='true'
        """)
        conn.commit()
        print("\n✅ AI Host enabled in database")
    except Exception as e:
        print(f"\n⚠️  Could not update database: {e}")
        print("   You can enable AI manually in the admin panel")
    finally:
        conn.close()

def check_model_dirs():
    """Check if AI models are downloaded."""
    base = Path(__file__).parent
    qwen_tts_dir = base / "Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    
    if qwen_tts_dir.exists():
        print(f"\n✅ Qwen3-TTS model found: {qwen_tts_dir}")
        print("   (LLM will be downloaded on first use)")
    else:
        print(f"\n⚠️  Qwen3-TTS model not found")
        print(f"   Expected at: {qwen_tts_dir}")
        print("\n   To download the model:")
        print(f"   1. pip install huggingface_hub")
        print(f"   2. huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign --local-dir {qwen_tts_dir}")
        print("\n   Or the AI will work without TTS (text-only mode)")

def main():
    print("=" * 60)
    print("  Radio TEDU AI Host Setup")
    print("=" * 60)
    
    check_python_version()
    
    if install_dependencies():
        enable_ai_in_database()
        check_model_dirs()
        
        print("\n" + "=" * 60)
        print("  ✅ AI Host setup complete!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Start the server: python run_cleanroom.py")
        print("2. The AI will generate announcements for music tracks")
        print("3. Check logs for AI activity")
        print("\nTo disable AI:")
        print("  Update station_settings SET value='false' WHERE key='ai_host_enabled'")
    else:
        print("\n❌ Setup failed. Please install dependencies manually.")
        sys.exit(1)

if __name__ == "__main__":
    main()
