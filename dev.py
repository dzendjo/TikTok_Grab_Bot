#!/usr/bin/env python3
"""Development server with hot-reload support."""
import sys
from pathlib import Path

try:
    from watchfiles import run_process
except ImportError:
    print("❌ watchfiles не встановлено!")
    print("Встановіть: pip install -r requirements-dev.txt")
    sys.exit(1)


def main():
    """Run bot with hot-reload."""
    watch_path = str(Path(__file__).parent / "app")
    
    print("🔥 Режим розробки з hot-reload")
    print(f"📁 Відстежується: {watch_path}")
    print("💡 Збережіть файл для автоматичного перезапуску")
    print("🛑 Ctrl+C для зупинки")
    print("=" * 60)
    
    run_process(
        watch_path,
        target=f"{sys.executable} -m app",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Розробку зупинено")
