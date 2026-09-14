"""Competition HTTP entry point: python main.py <port>."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "Demo/CoreGeek/main3.py"),
                   run_name="__main__")
